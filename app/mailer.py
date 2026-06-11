"""Email delivery, provider-agnostic via SMTP.

Configured entirely from the environment so any provider (SES, SendGrid,
Resend, Mailgun, a local relay) works through its SMTP interface:

    SMTP_HOST, SMTP_PORT (default 587), SMTP_USERNAME, SMTP_PASSWORD,
    SMTP_STARTTLS (default "1"), MAIL_FROM

When SMTP_HOST is unset, ConsoleMailer prints messages to stdout instead —
useful in development and a safe default in production misconfiguration
(nothing silently dropped: each message is logged).
"""

import os
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage


@dataclass
class Message:
    to: str
    subject: str
    text: str


class Mailer:
    def send(self, message: Message) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleMailer(Mailer):
    """Prints emails instead of sending them; also used by tests."""

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, message: Message) -> None:
        self.sent.append(message)
        print(f"[mail] to={message.to} subject={message.subject!r}")
        print(message.text)


@dataclass
class SMTPMailer(Mailer):
    host: str
    port: int = 587
    username: str | None = None
    password: str | None = None
    starttls: bool = True
    mail_from: str = "drugshortage@localhost"
    sent_count: int = field(default=0, init=False)

    def send(self, message: Message) -> None:
        email = EmailMessage()
        email["From"] = self.mail_from
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.text)

        with smtplib.SMTP(self.host, self.port, timeout=30) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.username:
                smtp.login(self.username, self.password or "")
            smtp.send_message(email)
        self.sent_count += 1


def get_mailer() -> Mailer:
    host = os.environ.get("SMTP_HOST")
    if not host:
        return ConsoleMailer()
    return SMTPMailer(
        host=host,
        port=int(os.environ.get("SMTP_PORT", "587")),
        username=os.environ.get("SMTP_USERNAME"),
        password=os.environ.get("SMTP_PASSWORD"),
        starttls=os.environ.get("SMTP_STARTTLS", "1") not in ("0", "false", "no"),
        mail_from=os.environ.get("MAIL_FROM", "drugshortage@localhost"),
    )
