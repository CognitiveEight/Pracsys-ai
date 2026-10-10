"""Redaction helpers for anything that may reach a log line or an error detail."""

from urllib.parse import urlsplit


def safe_url(url: str) -> str:
    """Host and path of a URL, without its query string.

    Signed storage URLs carry their credential in the query string (and some
    are valid for decades), so a URL is never logged whole.
    """
    try:
        parts = urlsplit(str(url or "").strip().rstrip('"'))
    except ValueError:
        return "<unparseable url>"
    if not parts.netloc:
        return "<no host>"
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def chars(value) -> str:
    """Stand-in for logged content: its size, never its text.

    Model output, prompts, user feedback and document text are case content
    (and often personal data); logs say how much there was, not what it said.
    """
    return f"<{len(str(value or ''))} chars>"
