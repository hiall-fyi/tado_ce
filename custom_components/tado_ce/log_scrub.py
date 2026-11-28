"""Scrub identifiers and secrets from the log records of Tado CE."""

from __future__ import annotations

import logging
import re

from .helpers import mask_response_body, mask_serial

_SECRET_PAIR_RE = re.compile(r"""(\b(?:refresh_token|access_token|id_token|auth_?key|client_secret)["']?\s*[:=]\s*["']?)(?!\[)[^\s"'&,;}\])]+""", re.IGNORECASE)
_JWT_RE = re.compile(r"\beyJ[\w.-]{5,}")
_BEARER_RE = re.compile(r"\bBearer\s+[\w.~+/=-]{8,}", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+")
_MAC_RE = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b")

_TRACEBACK = logging.Formatter()
_WITHHELD = "[log line withheld: it could not be scrubbed]"


def scrub_text(text: str) -> str:
    """Return text with secrets, emails, MAC-shaped ids, home ids and serials masked."""
    result = _SECRET_PAIR_RE.sub(r"\1[REDACTED]", text)
    result = _JWT_RE.sub("[TOKEN]", result)
    result = _BEARER_RE.sub("Bearer [TOKEN]", result)
    result = _EMAIL_RE.sub("[EMAIL]", result)
    result = _MAC_RE.sub(lambda match: mask_serial(match.group(0)), result)
    return mask_response_body(result, len(result))


class ScrubFilter(logging.Filter):
    """Rewrite a log record in place when its message or traceback holds something to mask."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Scrub the record in place and always let it through."""
        try:
            try:
                message = record.getMessage()
            except (TypeError, ValueError):
                # A malformed log call is reported by the handler that formats it, not here.
                return True
            scrubbed = scrub_text(message)
            if scrubbed != message:
                record.msg = scrubbed
                record.args = None
            trace = record.exc_text or (_TRACEBACK.formatException(record.exc_info) if record.exc_info else "")
            scrubbed_trace = scrub_text(trace)
            if scrubbed_trace != trace:
                record.exc_text = scrubbed_trace
            if record.stack_info:
                scrubbed_stack = scrub_text(record.stack_info)
                if scrubbed_stack != record.stack_info:
                    record.stack_info = scrubbed_stack
        except Exception:
            # A scrubber that fails must withhold the line, and it cannot log from inside the logging call.
            record.msg = _WITHHELD
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


class _ScrubHandler(logging.Handler):
    """Carry the scrub filter at the package logger; it emits nothing itself."""

    def emit(self, record: logging.LogRecord) -> None:
        """Do nothing: the filter has already run."""
        return None


def install_log_scrub(logger_name: str) -> None:
    """Attach one scrub handler to the named logger, once, so every child logger's record is scrubbed before the root handlers see it."""
    logger = logging.getLogger(logger_name)
    if any(isinstance(handler, _ScrubHandler) for handler in logger.handlers):
        return
    handler = _ScrubHandler()
    handler.addFilter(ScrubFilter())
    logger.addHandler(handler)
