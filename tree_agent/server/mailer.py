"""SMTP adapter used by account-recovery and administrator test-mail flows."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage


class SmtpMailer:
    def __init__(
        self, *, host: str, port: int, from_address: str, encryption: str = "none",
        username: str = "", password: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.from_address = from_address
        self.encryption = encryption
        self.username = username
        self.password = password

    def __call__(self, recipient: str, subject: str, text: str) -> None:
        message = EmailMessage()
        message["From"] = self.from_address
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(text)
        smtp_type = smtplib.SMTP_SSL if self.encryption == "ssl" else smtplib.SMTP
        with smtp_type(self.host, self.port, timeout=10) as smtp:
            if self.encryption == "starttls":
                smtp.ehlo()
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if self.username:
                # The password is supplied only to smtplib and is never logged.
                smtp.login(self.username, self.password or "")
            smtp.send_message(message)


# Compatibility for callers that construct the built-in local default directly.
LocalSmtpMailer = SmtpMailer
