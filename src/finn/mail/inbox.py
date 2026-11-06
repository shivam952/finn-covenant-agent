"""IMAP inbox polling and .eml parsing for incoming covenant check requests."""

from __future__ import annotations

import email
import email.policy
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class EmailAttachment:
    """An attachment extracted from an email message."""

    filename: str
    content_type: str
    payload: bytes


@dataclass
class ParsedEmail:
    """Parsed email fields and attachments."""

    sender: str
    subject: str
    message_id: str
    date: str
    body_text: str
    attachments: list[EmailAttachment] = field(default_factory=list)

    def first_xlsx_attachment(self) -> EmailAttachment | None:
        """Return the first attachment whose filename ends with .xlsx (case-insensitive)."""
        for att in self.attachments:
            if att.filename and att.filename.lower().endswith(".xlsx"):
                return att
        return None


def parse_eml_bytes(raw_bytes: bytes) -> ParsedEmail:
    """Parse raw .eml bytes using Python's stdlib email package (policy=default)."""
    msg = email.message_from_bytes(raw_bytes, policy=email.policy.default)
    sender = str(msg.get("From", "") or "").strip()
    subject = str(msg.get("Subject", "") or "").strip()
    message_id = str(msg.get("Message-ID", "") or "").strip()
    date = str(msg.get("Date", "") or "").strip()

    body_text = ""
    body_part = msg.get_body(preferencelist=("plain",))
    if body_part is not None:
        try:
            content = body_part.get_content()
            if isinstance(content, str):
                body_text = content.strip()
        except Exception:
            body_text = ""

    attachments: list[EmailAttachment] = []
    for part in msg.iter_attachments():
        fname = part.get_filename() or ""
        ctype = part.get_content_type() or "application/octet-stream"
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            attachments.append(
                EmailAttachment(
                    filename=fname,
                    content_type=ctype,
                    payload=payload,
                )
            )

    return ParsedEmail(
        sender=sender,
        subject=subject,
        message_id=message_id,
        date=date,
        body_text=body_text,
        attachments=attachments,
    )


def parse_eml_file(path: Path) -> ParsedEmail:
    """Read and parse a local .eml file."""
    return parse_eml_bytes(Path(path).read_bytes())
