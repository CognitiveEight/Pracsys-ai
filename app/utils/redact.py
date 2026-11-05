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


def validation_fields(err: BaseException) -> str:
    """Which fields failed validation, never their values.

    A pydantic ``ValidationError`` prints each offending ``input_value``, which
    for a socket payload is case content (document text, names, objectives).
    Logs get the field paths and error types only.
    """
    errors = getattr(err, "errors", None)
    if not callable(errors):
        return type(err).__name__
    try:
        parts = [
            "{}:{}".format(".".join(str(p) for p in e.get("loc", ())), e.get("type", ""))
            for e in errors()
        ]
    except Exception:
        return type(err).__name__
    return "{}[{}]".format(type(err).__name__, ", ".join(parts))
