"""Where generated draft Word files live, and how they are erased (SIM-211).

A draft's .docx is a hand-off to the backend, which copies it into its own
encrypted storage as the draft's first version. This service therefore keeps
the file only briefly and must be able to erase it with the workspace:

- Files are filed by workspace: ``drafts/<workspaceId>/<uuid>.docx``. The
  workspace purge deletes the whole prefix. The old layout,
  ``draft/<userId>/<uuid>-<title>.docx``, could not be found by a purge and put
  the draft title (which can name the parties) into the object name.
- The download link is short-lived (``DRAFT_DOCX_LINK_TTL_SECONDS``). Links
  that last for years cannot be revoked; the old ones (until 2050) are removed
  by ``scripts/cleanup_legacy_draft_files.py``.

Any new code that uploads a draft file must go through
:func:`upload_draft_docx`, or the purge will not reach it.
"""

from __future__ import annotations

import datetime as _dt
import re
import uuid
from typing import Optional

from loguru import logger

from app.core.config import settings
from app.storage import gcs as _gcs

DRAFT_PREFIX = "drafts"
# The pre-SIM-211 layout. Never written again; only the cleanup script reads it.
LEGACY_DRAFT_PREFIX = "draft"
DOCX_MIME = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

# Workspace ids are Mongo ObjectIds; allow the usual id alphabet and nothing
# that could escape the prefix ("/", "..").
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def draft_bucket() -> Optional[str]:
    """The bucket that holds draft files, or None when drafting export is off."""
    return getattr(settings, "gcs_docx_bucket_name", None) or None


def workspace_prefix(workspace_id: str) -> str:
    """``drafts/<workspaceId>/``: every draft file of one workspace."""
    if not workspace_id or not _SAFE_ID.match(workspace_id):
        raise ValueError("draft files need a valid workspace id")
    return f"{DRAFT_PREFIX}/{workspace_id}/"


def draft_blob_name(workspace_id: str) -> str:
    """A fresh object name for one draft file. No title, no user id."""
    return f"{workspace_prefix(workspace_id)}{uuid.uuid4().hex}.docx"


def link_expiry(now: Optional[_dt.datetime] = None) -> _dt.datetime:
    """When a draft link issued now stops working."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    ttl = int(getattr(settings, "draft_docx_link_ttl_seconds", 900) or 900)
    return now + _dt.timedelta(seconds=max(60, ttl))


def upload_draft_docx(workspace_id: str, data: bytes) -> str:
    """Upload one draft file and return its short-lived signed link.

    Blocking (GCS client); call through ``asyncio.to_thread``. Raises when the
    bucket is not configured or the workspace id is missing, so a draft is
    never written somewhere the purge cannot reach.
    """
    bucket = draft_bucket()
    if not bucket:
        raise RuntimeError("GCS_DOCX_BUCKET_NAME is not configured")
    blob_name = draft_blob_name(workspace_id)
    _gcs.upload_bytes(bucket, blob_name, data, DOCX_MIME)
    return _gcs.generate_signed_url(bucket, blob_name, expires_at=link_expiry())


def delete_workspace_draft_files(workspace_id: str) -> int:
    """Delete every draft file of a workspace; return how many were deleted.

    Blocking; call through ``asyncio.to_thread``. Raises on any storage error,
    so the purge retries and escalates instead of reporting success over files
    that still exist. With no bucket configured there is nothing to delete.
    """
    bucket = draft_bucket()
    if not bucket:
        return 0
    prefix = workspace_prefix(workspace_id)
    names = _gcs.list_blob_names(bucket, prefix)
    for name in names:
        _gcs.delete_blob_strict(bucket, name)
    if names:
        logger.info("Deleted {} draft file(s) for workspace {}", len(names), workspace_id)
    return len(names)
