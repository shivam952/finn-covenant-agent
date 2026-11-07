"""IMAP inbox polling and .eml parsing for incoming covenant check requests."""

from __future__ import annotations

import email
import email.policy
import imaplib
import ssl
import time
from datetime import datetime, timezone
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
    headers: dict[str, str] = field(default_factory=dict)  # lower-case header name -> value
    references: str = ""
    reply_to: str = ""

    @property
    def reply_address(self) -> str:
        """Where a reply goes: Reply-To if present, otherwise From."""
        return self.reply_to or self.sender

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

    headers = {str(k).lower(): str(v) for k, v in msg.items()}
    return ParsedEmail(
        sender=sender,
        subject=subject,
        message_id=message_id,
        date=date,
        body_text=body_text,
        attachments=attachments,
        headers=headers,
        references=str(msg.get("References", "") or "").strip(),
        reply_to=str(msg.get("Reply-To", "") or "").strip(),
    )


def parse_eml_file(path: Path) -> ParsedEmail:
    """Read and parse a local .eml file."""
    return parse_eml_bytes(Path(path).read_bytes())


@dataclass
class MailItem:
    """One message as listed by the mailbox."""

    uid: str
    raw: bytes
    received_at: datetime | None
    seen: bool


def _internaldate(meta: bytes) -> datetime | None:
    """Parse the INTERNALDATE from an IMAP FETCH response header (timezone-aware)."""
    tup = imaplib.Internaldate2tuple(meta)
    if tup is None:
        return None
    return datetime.fromtimestamp(time.mktime(tup), tz=timezone.utc)


class ImapMailbox:
    """Minimal IMAP client: list unseen messages, fetch them without marking, mark them seen."""

    def __init__(self, host: str, port: int, user: str, password: str, folder: str = "INBOX") -> None:
        self.host, self.port, self.user, self.password, self.folder = host, port, user, password, folder
        self._conn: imaplib.IMAP4_SSL | None = None

    def connect(self) -> None:
        conn = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ssl.create_default_context())
        conn.login(self.user, self.password)
        conn.select(self.folder)
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def _c(self) -> imaplib.IMAP4_SSL:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def fetch_recent(self, since: datetime) -> list[MailItem]:
        """Every message received on/after the day of `since`, read or unread (BODY.PEEK keeps flags).

        Finn does not rely on the unread flag: someone opening the inbox during a demo must not
        hide an order. The processed store decides what was already answered.
        """
        conn = self._c()
        conn.noop()  # refresh the mailbox state
        status, data = conn.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
        if status != "OK" or not data or not data[0]:
            return []
        out: list[MailItem] = []
        for uid in data[0].split():
            uid_s = uid.decode()
            st, parts = conn.uid("fetch", uid_s, "(FLAGS INTERNALDATE BODY.PEEK[])")
            if st != "OK" or not parts:
                continue
            for part in parts:
                if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
                    meta = part[0] if isinstance(part[0], bytes) else b""
                    seen = b"\\Seen" in meta
                    received = _internaldate(meta)
                    out.append(MailItem(uid=uid_s, raw=part[1], received_at=received, seen=seen))
                    break
        return out

    def mark_seen(self, uid: str) -> None:
        self._c().uid("store", uid, "+FLAGS", "(\\Seen)")
