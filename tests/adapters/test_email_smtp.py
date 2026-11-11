"""The sender that delivers sign-in links: the message and the SMTP conversation."""

import smtplib
from email.message import EmailMessage
from typing import Any

import pytest

from editta.adapters.email.smtp import MAGIC_LINK_SUBJECT, SMTPEmailSender


class _FakeSMTP:
    """Records the conversation instead of opening a socket."""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.steps: list[str] = []
        self.sent: EmailMessage | None = None
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *_: object) -> None:
        self.steps.append("quit")

    def starttls(self) -> None:
        self.steps.append("starttls")

    def login(self, username: str, password: str) -> None:
        self.steps.append(f"login:{username}")

    def send_message(self, message: EmailMessage) -> None:
        self.steps.append("send")
        self.sent = message


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[_FakeSMTP]:
    _FakeSMTP.instances.clear()
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    return _FakeSMTP


def _sender(**overrides: Any) -> SMTPEmailSender:
    return SMTPEmailSender(
        host="smtp.example.test",
        port=587,
        username="mailer",
        password="not-a-real-password",  # noqa: S106 -- test fixture
        from_address="noreply@editta.test",
        **overrides,
    )


@pytest.mark.asyncio
async def test_the_link_is_sent_encrypted_and_authenticated(smtp: type[_FakeSMTP]) -> None:
    link = "https://app.test/t/abc/auth/click?token=xyz"

    await _sender().send_magic_link(to_email="editor@press.test", link_url=link)

    (conversation,) = smtp.instances
    assert (conversation.host, conversation.port) == ("smtp.example.test", 587)
    # Credentials only after the connection is encrypted.
    assert conversation.steps == ["starttls", "login:mailer", "send", "quit"]
    message = conversation.sent
    assert message is not None
    assert (message["To"], message["From"], message["Subject"]) == (
        "editor@press.test",
        "noreply@editta.test",
        MAGIC_LINK_SUBJECT,
    )
    assert link in message.get_content()


@pytest.mark.asyncio
async def test_plain_smtp_can_be_chosen_for_a_local_relay(smtp: type[_FakeSMTP]) -> None:
    await _sender(use_tls=False).send_magic_link(to_email="a@b.test", link_url="https://x.test")

    assert "starttls" not in smtp.instances[0].steps


@pytest.mark.asyncio
async def test_a_recipient_with_a_newline_cannot_inject_headers(smtp: type[_FakeSMTP]) -> None:
    with pytest.raises(ValueError, match="(?i)header"):
        await _sender().send_magic_link(
            to_email="a@b.test\r\nBcc: victim@x.test", link_url="https://x.test"
        )

    assert smtp.instances == []  # nothing was sent
