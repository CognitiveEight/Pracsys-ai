"""
Centralised logging configuration (Loguru).

Every line carries the correlation context bound for the inbound event that
caused it — ``request_id``, ``event``, ``workspace_id`` — set once by
``SocketEventHandler.handle`` with ``logger.contextualize`` and inherited by
every task the event spawns (contextvars are copied by ``create_task``). To
follow one run, filter on its request id.

Sinks:

* console          — ``LOG_LEVEL``, coloured only on a terminal;
* logs/app.log     — ``LOG_LEVEL``, text, rotated and zipped;
* logs/errors.log  — ERROR and above;
* logs/structured.log — JSON (INFO+, plus trace records), for log shipping;
* logs/audit/audit.log — audit events only, kept a year.

Audit and trace records go only to their own sinks, so they never flood the
text logs. Tracebacks never include local variable values (``diagnose``)
unless ``LOG_DIAGNOSE=true`` — those values are payloads, document text and
credentials.

Rules for log calls are in CLAUDE.md ("Logging").
"""

import sys
from pathlib import Path

from loguru import logger

from app.core.config import settings

_TEXT_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
    "{extra[request_id]} {extra[event]} ws={extra[workspace_id]} | "
    "{name}:{function}:{line} - {message}"
)
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | "
    "<magenta>{extra[request_id]}</magenta> {extra[event]} ws={extra[workspace_id]} | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)
#: Values a line carries when it is not inside an inbound event (startup,
#: heartbeat, reconnect housekeeping).
CONTEXT_DEFAULTS = {"request_id": "-", "event": "-", "workspace_id": "-"}

_configured = False


def _is_plain(record) -> bool:
    extra = record["extra"]
    return not extra.get("audit") and not extra.get("trace")


def setup_logging(force: bool = False) -> None:
    """Configure all sinks. Idempotent: later calls are no-ops unless forced,
    so a second call can no longer remove sinks another module relies on."""
    global _configured
    if _configured and not force:
        return
    _configured = True

    logger.remove()
    logger.configure(extra=dict(CONTEXT_DEFAULTS))
    level = (settings.log_level or "INFO").upper()
    diagnose = bool(getattr(settings, "log_diagnose", False))

    logger.add(
        sys.stderr,
        format=_CONSOLE_FORMAT,
        level=level,
        colorize=sys.stderr.isatty(),
        backtrace=True,
        diagnose=diagnose,
        filter=_is_plain,
    )

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    file_opts = dict(enqueue=True, backtrace=True, diagnose=diagnose, compression="zip")

    logger.add(
        log_dir / "app.log",
        format=_TEXT_FORMAT,
        level=level,
        rotation="100 MB",
        retention="30 days",
        filter=_is_plain,
        **file_opts,
    )
    logger.add(
        log_dir / "errors.log",
        format=_TEXT_FORMAT,
        level="ERROR",
        rotation="50 MB",
        retention="90 days",
        filter=_is_plain,
        **file_opts,
    )
    logger.add(
        log_dir / "structured.log",
        format="{message}",
        level="INFO",
        rotation="100 MB",
        retention="30 days",
        serialize=True,
        filter=lambda r: not r["extra"].get("audit"),
        **file_opts,
    )
    if settings.enable_audit_log:
        audit_dir = log_dir / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            audit_dir / "audit.log",
            format="{message}",
            level="INFO",
            rotation="100 MB",
            retention="1 year",
            serialize=True,
            filter=lambda r: bool(r["extra"].get("audit")),
            **file_opts,
        )

    logger.debug("Logging configured (level={}, diagnose={})", level, diagnose)


# Configure on first import so modules that log at import time are covered.
setup_logging()
