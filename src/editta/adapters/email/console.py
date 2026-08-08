"""Dev-only EmailSender: logs the magic link instead of sending it. Swap for
SMTPEmailSender wherever real delivery matters (config.py picks the adapter).
"""

import structlog

logger = structlog.get_logger(__name__)


class ConsoleEmailSender:
    async def send_magic_link(self, *, to_email: str, link_url: str) -> None:
        logger.info("email.magic_link_console_only", to=to_email, link=link_url)
