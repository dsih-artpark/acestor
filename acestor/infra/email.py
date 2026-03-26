"""Email sending helpers for acestor."""

from __future__ import annotations

import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
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
    html_body: str | None = None,
    attachments: list[str | Path] | None = None,
) -> None:
    recip_list = list(recipients)

    msg = MIMEMultipart("mixed")
    msg["From"] = sender
    msg["To"] = ", ".join(recip_list)
    msg["Subject"] = subject

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(body, "plain"))
    if html_body:
        alt.attach(MIMEText(html_body, "html"))
    msg.attach(alt)

    for path in attachments or []:
        p = Path(path)
        if not p.exists():
            continue
        with p.open("rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename={p.name}")
        msg.attach(part)

    with smtplib.SMTP(host, port) as server:
        if use_tls:
            server.starttls()
        if username and password:
            server.login(username, password)
        server.sendmail(sender, recip_list, msg.as_string())
