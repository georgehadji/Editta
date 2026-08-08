"""SMTP EmailSender adapter — works with any SMTP-speaking provider (SES, Postmark,
Mailgun, SendGrid, ...) via stdlib smtplib. No vendor SDK, no new dependency.
"""

import asyncio
import smtplib
from email.message import EmailMessage

MAGIC_LINK_SUBJECT = "Your Editta sign-in link"


class SMTPEmailSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        use_tls: bool = True,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_address = from_address
        self._use_tls = use_tls

    async def send_magic_link(self, *, to_email: str, link_url: str) -> None:
        message = EmailMessage()
        message["Subject"] = MAGIC_LINK_SUBJECT
        message["From"] = self._from_address
        message["To"] = to_email
        message.set_content(
            "Click to sign in to Editta (link expires in 15 minutes):\n\n"
            f"{link_url}\n\n"
            "If you didn't request this, ignore this email."
        )
        await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: EmailMessage) -> None:
        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            if self._use_tls:
                smtp.starttls()
            smtp.login(self._username, self._password)
            smtp.send_message(message)
