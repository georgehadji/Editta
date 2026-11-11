"""The one logging setup. Every process calls `configure_logging` once, from its entrypoint.

Before this each entrypoint configured structlog on its own, and only the API removed
manuscript text from the log line; the worker, which handles the manuscript, did not.
The chain is: bound context (request, job, pass ids), level, time, service name, scrub,
JSON.
"""

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
            scrub_manuscript_content,
            structlog.processors.JSONRenderer(),
        ]
    )
