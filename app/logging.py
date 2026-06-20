import logging
import sys
from typing import Any, MutableMapping

import structlog
from structlog.types import Processor, WrappedLogger

# Header values starting with "Bearer " are scrubbed; case-insensitive
# scheme match. We slice the token to its 12-char prefix where applicable
# so ops debugging retains the prefix that identifies the key.
_BEARER_SCHEME_LOWER: str = "bearer "
_REDACTED: str = "<redacted>"
_PREFIX_LEN: int = 12  # mirrors app.auth.PREFIX_LEN; see §4.4


def _scrub(value: Any) -> str:
    """Replace a Bearer-bearing value with '<redacted: {prefix}>' when
    a 12-char prefix is extractable, else '<redacted>'."""
    if not isinstance(value, str):
        return _REDACTED
    stripped = value.lstrip()
    if stripped[:7].lower() == _BEARER_SCHEME_LOWER:
        token = stripped[7:].split()[0] if len(stripped) > 7 else ""
        if len(token) >= _PREFIX_LEN:
            return f"<redacted: {token[:_PREFIX_LEN]}>"
        return _REDACTED
    # Value matched the key rule but isn't itself a Bearer string;
    # scrub generically.
    return _REDACTED


def redact_authorization(
    logger: WrappedLogger,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Scrub Authorization-bearing fields from a structlog event.

    Two distinct redaction rules are applied in a single pass:

    1. KEY-MATCH: any key whose lowercase form equals "authorization"
       has its value replaced with the redacted form (see below),
       regardless of the value's actual content. This catches the
       common pattern `_log.info("auth check", authorization=header)`.
    2. VALUE-MATCH: any STRING value (not a key) that starts with
       "Bearer " (case-insensitive on the scheme) has its content
       replaced with the redacted form. This catches values that
       leak through under different keys (e.g. a logged headers dict
       or a stringified Request repr).

    Redacted form:
       - When the value contains a token whose first PREFIX_LEN chars
         are extractable, emit "<redacted: {prefix}>" so ops debugging
         retains the prefix that ties the log line to a key in keys.db.
       - Otherwise emit the literal "<redacted>".

    Mutation policy: edits the event_dict in place AND returns it
    (structlog protocol). Nested dicts and lists are NOT walked
    recursively in v0.2.0 — only top-level keys and values are
    inspected.
    """
    for key in list(event_dict.keys()):
        value = event_dict[key]

        # Rule 1: key-match (case-insensitive).
        if key.lower() == "authorization":
            event_dict[key] = _scrub(value)
            continue

        # Rule 2: value-match — only on string values.
        if isinstance(value, str):
            stripped = value.lstrip()
            if stripped[:7].lower() == _BEARER_SCHEME_LOWER:
                event_dict[key] = _scrub(value)
    return event_dict


def configure_structlog(level: str) -> None:
    """Configure structlog + stdlib logging bridge for the gateway.

    All gateway log lines (structlog-emitted AND stdlib-`logging`-emitted
    via existing `logging.getLogger(...)` call sites) flow through the
    same processor chain and render as one-line JSON to stdout.

    Args:
        level: log level name; case-insensitive ("INFO", "info", "DEBUG"
            etc). Parsed via logging.getLevelNamesMapping(). Unknown
            levels fall back to logging.INFO.
    """
    level_int: int = logging.getLevelNamesMapping().get(
        level.upper(), logging.INFO
    )

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
        structlog.processors.EventRenamer(to="event"),
        redact_authorization,
    ]

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level_int),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace any handlers added by a prior basicConfig() call so we
    # don't double-emit.
    root.handlers = [handler]
    root.setLevel(level_int)
