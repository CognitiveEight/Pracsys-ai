"""
Replay-resistant handshake authentication for the backend Socket.IO connection.

Scheme ``v1`` (see ``docs/AI_SERVICE_AUTH_MIGRATION.md``) signs a canonical
string that carries a UTC timestamp and a single-use random nonce::

    v1:AI_Maverick:<timestamp>:<nonce>

The backend refuses a timestamp outside +/-300s of its own clock and refuses any
nonce it has already recorded, so a captured handshake is worthless: it goes
stale within minutes and cannot be used even twice inside those minutes.

Three headers carry the proof, alongside the unchanged ``x-client-type``:

===========================  ==================================================
``x-signature``              HMAC-SHA256, base64url (RFC 4648 section 5),
                             padding stripped -- always exactly 43 characters
``x-signature-timestamp``    UTC Unix time in whole seconds, digits only
``x-signature-nonce``        32 lowercase hex characters (16 random bytes)
===========================  ==================================================

Two hard rules, both of which the backend enforces and neither of which has a
grace period:

1. The header values must be **character-identical** to the canonical fields.
   Build the canonical string once and reuse those exact strings.
2. Headers are **single-use**. :func:`build_auth_headers` must be called again
   for every connection attempt, including every automatic reconnect --
   ``app/socket/client.py`` does that by handing the builder itself to
   python-socketio, which re-resolves callable headers on each attempt.

The legacy scheme (a signature over a constant payload, therefore a fixed value
valid forever) is retained only so ``SOCKET_AUTH_SCHEME=legacy`` can roll the
service back inside the backend's ``AI_SIGNATURE_ALLOW_LEGACY`` window without a
redeploy. It must not be the default and the window must be closed promptly.

Standard library only -- no third-party dependency is needed to build headers.
"""

import base64
import hashlib
import hmac
import secrets
import time
from typing import Dict, Optional

from loguru import logger

# ---------------------------------------------------------------------------
# Contract constants. Changing any of these breaks the handshake -- they are
# agreed with the backend and a change means a new scheme version.
# ---------------------------------------------------------------------------
SCHEME_V1 = "v1"
SCHEME_LEGACY = "legacy"

CLIENT_ID = "AI_Maverick"
DELIMITER = ":"

HEADER_CLIENT_TYPE = "x-client-type"
HEADER_SIGNATURE = "x-signature"
HEADER_TIMESTAMP = "x-signature-timestamp"
HEADER_NONCE = "x-signature-nonce"

#: Bytes of nonce entropy; 16 bytes -> exactly 32 lowercase hex characters.
NONCE_BYTES = 16

#: Published worked example from the migration document, used by :func:`self_check`.
_SELF_CHECK_SECRET = "REPLACE_ME_placeholder"
_SELF_CHECK_TIMESTAMP = "1755763200"
_SELF_CHECK_NONCE = "0f3a1c9d2b47e6580a1b2c3d4e5f6071"
_SELF_CHECK_CANONICAL = "v1:AI_Maverick:1755763200:0f3a1c9d2b47e6580a1b2c3d4e5f6071"
_SELF_CHECK_SIGNATURE = "3G0xegMwS8d5QOnJOA759UxyxeggnzrdP-TIZy1gOSM"


class SocketAuthError(RuntimeError):
    """Raised when handshake headers cannot be built (e.g. missing secret)."""


def build_canonical_string(timestamp: str, nonce: str) -> str:
    """
    Join the four canonical fields with a single ASCII colon.

    No sorting, no key names, no JSON, no leading/trailing delimiter, no
    trailing newline and no whitespace: the result must be exactly
    ``v1:AI_Maverick:<timestamp>:<nonce>``.

    Args:
        timestamp: UTC Unix seconds as decimal digits, e.g. ``"1755763200"``.
        nonce: 32 lowercase hexadecimal characters.

    Returns:
        The exact string to feed to HMAC.
    """
    return DELIMITER.join([SCHEME_V1, CLIENT_ID, timestamp, nonce])


def sign_canonical(secret: str, canonical: str) -> str:
    """
    HMAC-SHA256 the canonical string and encode it the way the backend expects.

    Encoding is pinned: base64url (the ``-`` / ``_`` alphabet) with padding
    stripped, never standard base64 and never hex. The result is always 43
    characters. This matches Node's ``digest('base64url')`` byte for byte.

    Args:
        secret: The shared ``SOCKET_SECRET_KEY``. Never logged.
        canonical: Output of :func:`build_canonical_string`.

    Returns:
        The 43-character unpadded base64url signature.
    """
    digest = hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def generate_nonce() -> str:
    """
    Return a fresh 32-character lowercase-hex nonce (128 bits).

    Uses :mod:`secrets`, never :mod:`random`: a predictable nonce lets an
    attacker pre-spend the values this client is about to use.
    """
    return secrets.token_hex(NONCE_BYTES)


def current_timestamp() -> str:
    """
    Return UTC Unix time in whole seconds as decimal digits.

    ``int()`` truncates the float from :func:`time.time`; a decimal point in
    this value is the single most common cause of ``bad_signature``. The system
    clock must be NTP-synchronised -- drift beyond +/-300s fails every
    connection with ``stale_timestamp``.
    """
    return str(int(time.time()))


def build_v1_auth_headers(secret: str) -> Dict[str, str]:
    """
    Build a fresh set of ``v1`` handshake headers.

    MUST be called immediately before every connection attempt, including every
    automatic reconnect: the nonce is single-use and the timestamp expires.

    Args:
        secret: The shared ``SOCKET_SECRET_KEY``.

    Returns:
        The four handshake headers, lowercase-keyed.

    Raises:
        SocketAuthError: If ``secret`` is empty.
    """
    if not secret:
        raise SocketAuthError(
            "SOCKET_SECRET_KEY is not set; cannot sign the socket handshake"
        )

    timestamp = current_timestamp()
    nonce = generate_nonce()
    canonical = build_canonical_string(timestamp, nonce)
    signature = sign_canonical(secret, canonical)

    # The header values are the same objects used to build the canonical string,
    # so they cannot drift from what was signed.
    return {
        HEADER_CLIENT_TYPE: CLIENT_ID,
        HEADER_SIGNATURE: signature,
        HEADER_TIMESTAMP: timestamp,
        HEADER_NONCE: nonce,
    }


def build_legacy_auth_headers(secret: str, payload: Optional[str]) -> Dict[str, str]:
    """
    Build the pre-migration handshake headers (rollback path only).

    The signature is HMAC-SHA256 over a constant payload, so it never varies and
    is replayable for as long as the secret lives -- that is the vulnerability
    this migration closes. Only reachable via ``SOCKET_AUTH_SCHEME=legacy``, and
    only accepted while the backend's ``AI_SIGNATURE_ALLOW_LEGACY`` window is
    open. Note this path emits *padded* standard-alphabet-safe base64, matching
    what the backend's legacy workaround tolerates.

    Args:
        secret: The shared ``SOCKET_SECRET_KEY``.
        payload: ``SOCKET_AI_PAYLOAD`` -- the constant AI-user JSON string.

    Returns:
        ``x-client-type`` plus the legacy ``x-signature``.

    Raises:
        SocketAuthError: If the secret or the payload is missing.
    """
    if not secret:
        raise SocketAuthError(
            "SOCKET_SECRET_KEY is not set; cannot sign the socket handshake"
        )
    if not payload:
        raise SocketAuthError(
            "SOCKET_AI_PAYLOAD is required for the legacy signature scheme"
        )

    digest = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return {
        HEADER_CLIENT_TYPE: CLIENT_ID,
        HEADER_SIGNATURE: base64.urlsafe_b64encode(digest).decode("ascii"),
    }


def build_auth_headers(
    secret: str,
    scheme: str = SCHEME_V1,
    legacy_payload: Optional[str] = None,
) -> Dict[str, str]:
    """
    Build handshake headers for the configured scheme.

    Args:
        secret: The shared ``SOCKET_SECRET_KEY``.
        scheme: ``"v1"`` (default) or ``"legacy"``; compared case-insensitively.
        legacy_payload: ``SOCKET_AI_PAYLOAD``, required only for ``"legacy"``.

    Returns:
        A fresh set of handshake headers.

    Raises:
        SocketAuthError: On a missing secret, a missing legacy payload, or an
            unknown scheme name (which is refused rather than silently
            downgraded).
    """
    normalized = (scheme or SCHEME_V1).strip().lower()
    if normalized == SCHEME_V1:
        return build_v1_auth_headers(secret)
    if normalized == SCHEME_LEGACY:
        return build_legacy_auth_headers(secret, legacy_payload)
    raise SocketAuthError(
        f"Unknown socket auth scheme {scheme!r}; expected "
        f"{SCHEME_V1!r} or {SCHEME_LEGACY!r}"
    )


def redact_auth_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """
    Copy headers with the signature masked, for logging.

    The timestamp and nonce are deliberately kept: the backend asks for both
    when diagnosing a refused handshake, and neither is a credential. The
    signature and the secret must never reach a log.
    """
    return {
        key: "***" if key == HEADER_SIGNATURE else value
        for key, value in headers.items()
    }


def self_check() -> None:
    """
    Verify this build against the worked example published by the backend.

    Run before touching the socket client: a mismatch here is the same
    ``bad_signature`` the handshake would produce, reported in one second
    instead of at 2am.

    Raises:
        AssertionError: If the canonical string or the signature does not match.
    """
    canonical = build_canonical_string(_SELF_CHECK_TIMESTAMP, _SELF_CHECK_NONCE)
    assert canonical == _SELF_CHECK_CANONICAL, f"canonical mismatch: {canonical!r}"
    assert len(canonical) == 58, (
        f"canonical string is {len(canonical)} chars, expected 58 "
        "(59 means a trailing newline)"
    )

    signature = sign_canonical(_SELF_CHECK_SECRET, canonical)
    assert signature == _SELF_CHECK_SIGNATURE, (
        f"signature mismatch: got {signature}, expected {_SELF_CHECK_SIGNATURE}"
    )
    assert len(signature) == 43, "signature must be 43 unpadded base64url characters"
    assert "=" not in signature, "signature must not be padded"

    nonce = generate_nonce()
    assert len(nonce) == 32 and all(
        c in "0123456789abcdef" for c in nonce
    ), f"nonce must be 32 lowercase hex characters, got {nonce!r}"

    timestamp = current_timestamp()
    assert timestamp.isdigit() and len(timestamp) <= 15, (
        f"timestamp must be <=15 decimal digits, got {timestamp!r}"
    )

    logger.success("socket auth self-check OK (scheme {})", SCHEME_V1)


if __name__ == "__main__":
    self_check()
    print("self-check OK")
