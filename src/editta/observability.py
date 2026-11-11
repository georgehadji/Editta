"""The one logging setup. Every process calls `configure_logging` once, from its entrypoint.

Before this each entrypoint configured structlog on its own, and only the API removed
manuscript text from the log line; the worker, which handles the manuscript, did not.
The chain is: bound context (request, job, pass ids), level, time, service name,
exception summary, scrub, JSON.
"""

import sys
import traceback
from collections.abc import Mapping, MutableMapping
from typing import Any, Final

import structlog

# A log field longer than this is not log data. Honest fields (ids, error types, counts)
# are far shorter; a manuscript passage is not.
MAX_FIELD_CHARS: Final = 2_000
_CONTENT_KEYS: Final = ("text", "manuscript_block", "content")
_DROPPED: Final = "[dropped: too large to be log data]"


def scrub_manuscript_content(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """Never let manuscript text reach logs, even by accident (PLAN.md §5). Known content
    keys go; so does any raw bytes or very long string, whatever its key."""
    for key in _CONTENT_KEYS:
        event_dict.pop(key, None)
    for key, value in event_dict.items():
        if isinstance(value, bytes) or (isinstance(value, str) and len(value) > MAX_FIELD_CHARS):
            event_dict[key] = _DROPPED
    return event_dict


def summarize_exception(
    _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
) -> Mapping[str, Any]:
    """`logger.exception(...)` records what failed and where: the exception's type and its
    deepest frame. Never its message, which could quote the manuscript."""
    exc_info = event_dict.pop("exc_info", None)
    if exc_info is True:
        exc_info = sys.exc_info()
    elif isinstance(exc_info, BaseException):
        exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
    if isinstance(exc_info, tuple) and exc_info[0] is not None:
        event_dict["error_type"] = exc_info[0].__name__
        frames = traceback.extract_tb(exc_info[2])
        if frames:
            event_dict["error_at"] = f"{frames[-1].filename}:{frames[-1].lineno}"
    return event_dict


def _add_service(service: str) -> structlog.typing.Processor:
    def add(
        _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
    ) -> Mapping[str, Any]:
        event_dict["service"] = service
        return event_dict

    return add


def configure_logging(service: str) -> None:
    """`service` names the process ("api", "worker", "retention") in every line."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_service(service),
            summarize_exception,
            scrub_manuscript_content,
            structlog.processors.JSONRenderer(),
        ]
    )
