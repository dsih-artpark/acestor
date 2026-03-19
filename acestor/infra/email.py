"""Email sending helpers for acestor."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Iterable


def send_email(
    host: str,
    port: int,
    username: str | None,
    password: str | None,
    use_tls: bool,
    sender: str,
    recipients: Iterable[str],
    subject: str,
    body: str,
) -> None:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    if use_tls:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            if username and password:
                server.login(username, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port) as server:
            if username and password:
                server.login(username, password)
            server.send_message(msg)
