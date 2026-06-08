"""Generic Google Cloud Storage helpers.

Single source of truth for GCS auth and uploads used by both the audio
transcription pipeline and the drafting-assistant DOCX export. Credentials
are resolved in the same order the audio path uses, so a single service
account configured via ``GOOGLE_APPLICATION_CREDENTIALS`` (or its
``SPEECH_*`` override) covers all GCS work.

Helpers are intentionally synchronous; call them from ``asyncio.to_thread``
when invoked from coroutines.

**URL strategy (UBLA-safe).** This module never calls ``blob.make_public()``
— that path uses legacy per-object ACLs and breaks under Uniform Bucket-
Level Access. Long-lived links are produced via V2 signed URLs
(``generate_signed_url`` below). V2 has no 7-day cap (V4 does), so the
URLs can be valid for years — matching the platform's existing TypeScript
upload service.
"""

from __future__ import annotations

import datetime as _dt
import os
from typing import Optional, Tuple, Union

from loguru import logger

from app.core.config import settings


_CACHED_CREDS_PATH: Optional[str] = None
_CREDS_RESOLVED: bool = False


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _candidate_base_dirs() -> list[str]:
    bases = [os.getcwd()]
    here = os.path.abspath(os.path.dirname(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    if repo_root not in bases:
        bases.append(repo_root)
    return bases


def _is_service_account_json(path: str) -> bool:
    try:
        import json

        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except Exception:
        return False
    return isinstance(obj, dict) and obj.get("type") == "service_account"


def _resolve_credentials_path() -> Optional[str]:
    """Locate a usable service-account JSON.

    Resolution order — first non-empty wins:
      1. ``GOOGLE_APPLICATION_CREDENTIALS`` env / settings (primary).
      2. ``SPEECH_GOOGLE_APPLICATION_CREDENTIALS`` env / settings (fallback
         — the audio SA usually has Storage perms too).

    Relative paths resolve against cwd then the repo root. The resolved
    path is **not** written back to ``os.environ``; the credentials are
    passed explicitly to ``storage.Client`` so other paths (OCR / Speech)
    keep using their own configuration.
    """
    global _CACHED_CREDS_PATH, _CREDS_RESOLVED
    if _CREDS_RESOLVED:
        return _CACHED_CREDS_PATH

    raw_candidates = [
        getattr(settings, "google_application_credentials", None),
        os.getenv("GOOGLE_APPLICATION_CREDENTIALS"),
        getattr(settings, "speech_google_application_credentials", None),
        os.getenv("SPEECH_GOOGLE_APPLICATION_CREDENTIALS"),
    ]
    bases = _candidate_base_dirs()

    resolved: Optional[str] = None
    for raw in raw_candidates:
        if not raw:
            continue
        candidates: list[str] = []
        if os.path.isabs(raw):
            candidates.append(raw)
        else:
            candidates.extend(os.path.join(base, raw) for base in bases)
        for cand in candidates:
            if os.path.exists(cand) and _is_service_account_json(cand):
                resolved = os.path.abspath(cand)
                break
        if resolved:
            break

    if resolved:
        logger.info("GCS: using service account at {}", resolved)
    else:
        logger.warning(
            "GCS: no service-account JSON resolved. Falling back to "
            "Application Default Credentials, which may fail for uploads."
        )

    _CACHED_CREDS_PATH = resolved
    _CREDS_RESOLVED = True
    return resolved


def _load_service_account_credentials():
    path = _resolve_credentials_path()
    if not path:
        return None
    try:
        from google.oauth2 import service_account  # type: ignore
    except ImportError:
        return None
    try:
        return service_account.Credentials.from_service_account_file(path)
    except Exception as exc:
        logger.warning("GCS: failed to load SA credentials: {}", exc)
        return None


def _resolve_project_id() -> Optional[str]:
    return (
        os.getenv("GOOGLE_CLOUD_PROJECT")
        or getattr(settings, "gcp_project_id", None)
        or os.getenv("GCP_PROJECT_ID")
    )


# ---------------------------------------------------------------------------
# Client / URL helpers
# ---------------------------------------------------------------------------


def get_gcs_client():
    """Return an authenticated ``google.cloud.storage.Client``."""
    from google.cloud import storage  # type: ignore

    creds = _load_service_account_credentials()
    project = _resolve_project_id()
    if creds is not None:
        return storage.Client(project=project, credentials=creds)
    return storage.Client(project=project)


def public_url(bucket: str, blob_name: str) -> str:
    """Return the canonical public HTTPS URL for a blob."""
    return f"https://storage.googleapis.com/{bucket}/{blob_name}"


def parse_gs_uri(uri: str) -> Tuple[str, str]:
    """Split ``gs://bucket/path/to/blob`` into ``(bucket, blob_name)``."""
    if not uri or not uri.startswith("gs://"):
        raise ValueError(f"Invalid gs:// URI: {uri!r}")
    without_scheme = uri[len("gs://"):]
    bucket, _, blob_name = without_scheme.partition("/")
    if not bucket or not blob_name:
        raise ValueError(f"Invalid gs:// URI: {uri!r}")
    return bucket, blob_name


# ---------------------------------------------------------------------------
# Upload / download
# ---------------------------------------------------------------------------


def upload_bytes(
    bucket: str,
    blob_name: str,
    data: bytes,
    content_type: str,
    *,
    public: bool = False,
) -> str:
    """Upload bytes to ``gs://{bucket}/{blob_name}``.

    The ``public`` flag is retained for callsite intent / readability but
    is intentionally a no-op — public access is handled via long-lived
    signed URLs (see :func:`generate_signed_url`) so the upload works
    under Uniform Bucket-Level Access.
    """
    _ = public  # accepted for API stability, see docstring
    client = get_gcs_client()
    blob = client.bucket(bucket).blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{bucket}/{blob_name}"


def download_bytes(bucket: str, blob_name: str) -> bytes:
    """Download a blob's contents as raw bytes."""
    client = get_gcs_client()
    return client.bucket(bucket).blob(blob_name).download_as_bytes()


def generate_signed_url(
    bucket: str,
    blob_name: str,
    *,
    expires_at: Optional[Union[_dt.datetime, str]] = None,
) -> str:
    """Generate a long-lived V2 signed URL for a blob.

    Mirrors the platform's TypeScript upload service which signs with
    ``expires: '2050-01-01T00:00:00Z'``. V2 signing (unlike V4) has no
    7-day cap, so the link remains valid for the configured horizon.

    Requires the service-account credentials to carry a usable private
    key (the JSON file already does — no IAM Credentials API needed).

    Args:
        bucket: bucket name.
        blob_name: object key inside the bucket.
        expires_at: a ``datetime`` (timezone-aware preferred) or ISO-8601
            string. Defaults to ``2050-01-01T00:00:00Z`` to match the TS
            convention.
    """
    if expires_at is None:
        expires_at = _dt.datetime(2050, 1, 1, tzinfo=_dt.timezone.utc)
    elif isinstance(expires_at, str):
        # Accept ISO with or without trailing Z.
        s = expires_at.rstrip("Z")
        expires_at = _dt.datetime.fromisoformat(s).replace(
            tzinfo=_dt.timezone.utc
        )

    client = get_gcs_client()
    blob = client.bucket(bucket).blob(blob_name)
    return blob.generate_signed_url(
        version="v2",
        expiration=expires_at,
        method="GET",
    )


def delete_blob(bucket: str, blob_name: str) -> None:
    """Best-effort delete; swallows not-found errors."""
    try:
        client = get_gcs_client()
        client.bucket(bucket).blob(blob_name).delete()
    except Exception as exc:
        logger.debug("GCS delete failed for gs://{}/{}: {}", bucket, blob_name, exc)
