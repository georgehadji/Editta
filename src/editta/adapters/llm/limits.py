"""Time limits shared by both LLM adapters, so they cannot drift apart."""

from typing import Final

# One model request. A book-length prompt with high-effort thinking can take minutes to
# answer; the streamed read timeout restarts with every chunk that arrives.
REQUEST_TIMEOUT_SECONDS: Final = 600.0
