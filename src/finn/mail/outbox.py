"""Build reply MIME messages (threaded) and send them via SMTP."""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

from finn.report import Reply


def build_reply_message(
    reply: Reply,
    *,
    from_addr: str,
    from_name: str,
    to_addr: str,
    cc_addrs: list[str] | None = None,
    in_reply_to: str = "",
    references: str = "",
    attachments: list[tuple[str, bytes, str]] | None = None,
) -> EmailMessage:
    """A multipart/alternative (text + HTML) reply in the same thread, with optional attachments."""
    msg = EmailMessage()
    msg["Subject"] = reply.subject
    msg["From"] = formataddr((from_name, from_addr)) if from_name else from_addr
    msg["To"] = to_addr
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Date"] = formatdate(localtime=True)
    domain = parseaddr(from_addr)[1].split("@")[-1] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = f"{references} {in_reply_to}".strip() if references else in_reply_to
        msg["References"] = refs
    # Marker header so Finn can recognise (and ignore) its own replies.
    msg["X-Finn-Reply"] = "1"
    msg.set_content(reply.text)
    msg.add_alternative(reply.html, subtype="html")
    for filename, payload, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(payload, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    return msg


def send_message(
    msg: EmailMessage,
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    timeout_s: float = 30.0,
) -> None:
    """Send via SMTP over SSL (port 465) or STARTTLS (any other port)."""
    context = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=timeout_s) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=timeout_s) as smtp:
            smtp.starttls(context=context)
            smtp.login(user, password)
            smtp.send_message(msg)
