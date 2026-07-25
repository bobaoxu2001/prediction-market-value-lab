"""Logging with secret redaction.

Provider payloads and config objects flow through log statements during ingest. A
filter strips anything that looks like a key so a stray ``logger.debug(payload)``
can never leak a credential into a log file.
"""

from __future__ import annotations

import logging
import re
import sys

from .config import get_settings

_SECRET_PATTERNS = [
    re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----)"),
    re.compile(r"(sk-ant-[A-Za-z0-9\-_]{6})[A-Za-z0-9\-_]+"),
    re.compile(r"((?:api[_-]?key|secret|token|password|private[_-]?key)\"?\s*[:=]\s*\"?)([^\s\"',]{6,})",
               re.IGNORECASE),
]


def redact(text: str) -> str:
    text = _SECRET_PATTERNS[0].sub(r"\1***REDACTED***\2", text)
    text = _SECRET_PATTERNS[1].sub(r"\1***", text)
    text = _SECRET_PATTERNS[2].sub(r"\1***REDACTED***", text)
    return text


class _RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            if record.args:
                record.args = tuple(
                    redact(a) if isinstance(a, str) else a for a in _as_tuple(record.args)
                )
        except Exception:  # noqa: BLE001 - logging must never raise
            pass
        return True


def _as_tuple(args):  # noqa: ANN001, ANN202
    return args if isinstance(args, tuple) else (args,)


_configured = False


def setup_logging(level: str | None = None) -> None:
    global _configured
    if _configured:
        return
    settings = get_settings()
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)-34s %(message)s", "%H:%M:%S")
    )
    handler.addFilter(_RedactionFilter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level or settings.log_level)
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
