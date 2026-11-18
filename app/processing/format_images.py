"""Download a letterhead image for a chosen format (SIM-217).

The backend sends a letterhead's logo or banner as a link. This module is the
only place the drafting path fetches one, and it is deliberately strict:

- https only, no redirects (a short-lived signed storage link never redirects);
- the host must resolve to a public address (no loopback, private, link-local
  or reserved ranges), so a format cannot make this service call itself or the
  cloud metadata server;
- about 10 seconds and 5 MB at most;
- PNG or JPEG only, checked by the file's own first bytes, not its headers.

Any failure returns ``None``: the caller skips that image and the draft is
still delivered. Nothing is kept: no link, no file. Logs carry ``safe_url``
only.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urlsplit

from loguru import logger

from app.utils.redact import safe_url

FETCH_TIMEOUT_SECONDS = 10.0
MAX_IMAGE_BYTES = 5 * 1024 * 1024

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def image_kind(data: bytes) -> Optional[str]:
    """``"png"`` or ``"jpeg"`` from the file's own first bytes, else None."""
    if not isinstance(data, (bytes, bytearray)):
        return None
    if data.startswith(_PNG_MAGIC):
        return "png"
    if data.startswith(_JPEG_MAGIC):
        return "jpeg"
    return None


def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            addr.is_private
            or addr.is_loopback
            or addr.is_link_local
            or addr.is_multicast
            or addr.is_reserved
            or addr.is_unspecified
        ):
            return False
    return True


def fetch_format_image(url: str) -> Optional[bytes]:
    """Fetch one letterhead image; ``None`` on any failure. Blocking."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname:
        logger.info("format image skipped: not an https link {}", safe_url(url))
        return None
    if not _host_is_public(parts.hostname):
        logger.info("format image skipped: host is not public {}", safe_url(url))
        return None
    try:
        import httpx

        with httpx.Client(
            timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False
        ) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.info(
                        "format image fetch failed status={} {}",
                        resp.status_code,
                        safe_url(url),
                    )
                    return None
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > MAX_IMAGE_BYTES:
                    logger.info("format image too large {}", safe_url(url))
                    return None
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf.extend(chunk)
                    if len(buf) > MAX_IMAGE_BYTES:
                        logger.info("format image too large {}", safe_url(url))
                        return None
    except Exception as exc:
        logger.info(
            "format image fetch failed error={} {}", type(exc).__name__, safe_url(url)
        )
        return None
    data = bytes(buf)
    if image_kind(data) is None:
        logger.info("format image is not PNG or JPEG {}", safe_url(url))
        return None
    return data
