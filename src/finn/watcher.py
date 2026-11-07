"""`finn watch`: poll the mailbox, answer every new mail exactly once, never crash.

Exactly-once: a Message-ID is recorded in the processed store only after its reply was sent
(or it was deliberately ignored). A failed send is retried on later polls up to a limit, then
given up and logged. Connection errors cause a reconnect on the next poll.
"""

from __future__ import annotations

import json
import logging
import time
import email
import email.policy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from finn.config import Settings
from finn.handler import Handled, handle_email
from finn.intake import load_agent_config
from finn.llm import JsonLLM
from finn.mail.inbox import MailItem, parse_eml_bytes

log = logging.getLogger("finn.watch")


class Mailbox(Protocol):
    def fetch_recent(self, since: datetime) -> list[MailItem]: ...
    def mark_seen(self, uid: str) -> None: ...
    def close(self) -> None: ...


Sender = Callable[[Any], None]  # takes an EmailMessage and sends it


class ProcessedStore:
    """JSON file: message-id -> {status, attempts, run_id, at}."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.data: dict[str, dict[str, Any]] = {}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                log.warning("processed store unreadable, starting empty: %s", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def is_done(self, key: str) -> bool:
        return self.data.get(key, {}).get("status") in ("sent", "ignored", "given_up")

    def attempts(self, key: str) -> int:
        return int(self.data.get(key, {}).get("attempts", 0))

    def record(self, key: str, status: str, **extra: Any) -> None:
        entry = self.data.get(key, {})
        entry.update(status=status, at=datetime.now(timezone.utc).isoformat(), **extra)
        self.data[key] = entry
        self._save()


def _message_key(raw: bytes, parsed_id: str, uid: str) -> str:
    return parsed_id or f"uid:{uid}:{len(raw)}"


def poll_once(
    mailbox: Mailbox,
    send: Sender,
    settings: Settings,
    llm: JsonLLM | None,
    store: ProcessedStore,
    *,
    max_send_attempts: int,
    started_at: datetime | None = None,
    lookback_days: int = 1,
    start_grace_minutes: int = 15,
) -> list[Handled]:
    """Answer every recent mail that is not yet in the store. Returns what was handled.

    A mail counts as new if it is unread, or if it arrived after the watcher started (minus a
    grace period) even when someone already opened it. Older read mail is left alone.
    """
    now = datetime.now(timezone.utc)
    started_at = started_at or now
    since = now - timedelta(days=lookback_days)
    new_after = started_at - timedelta(minutes=start_grace_minutes)
    handled: list[Handled] = []
    for item in mailbox.fetch_recent(since):
        try:
            parsed = parse_eml_bytes(item.raw)
        except Exception as exc:
            log.error("cannot parse message uid=%s: %s", item.uid, exc)
            continue
        key = _message_key(item.raw, parsed.message_id, item.uid)
        if store.is_done(key):
            continue
        is_new = (not item.seen) or (item.received_at is not None and item.received_at >= new_after)
        if not is_new and key not in store.data:
            continue  # old, already read mail from before the start: not an order for this session

        entry = store.data.get(key, {})
        saved = entry.get("reply_eml")
        if entry.get("status") == "send_failed" and saved and Path(saved).exists():
            message = email.message_from_bytes(Path(saved).read_bytes(), policy=email.policy.default)
            run_id = entry.get("run_id")
            decision = entry.get("decision")
        else:
            h = handle_email(parsed, settings, llm)
            handled.append(h)
            if h.ignored_reason is not None:
                log.info("ignored %s (%s)", key, h.ignored_reason)
                store.record(key, "ignored", reason=h.ignored_reason)
                mailbox.mark_seen(item.uid)
                continue
            message = h.message
            run_id = h.run_id
            decision = h.decision.kind if h.decision else None
            saved = str(h.files.get("reply.eml", ""))

        try:
            send(message)
        except Exception as exc:
            attempts = store.attempts(key) + 1
            if attempts >= max_send_attempts:
                log.error("giving up sending reply to %s after %d attempts: %s", key, attempts, exc)
                store.record(key, "given_up", attempts=attempts, run_id=run_id, error=str(exc), reply_eml=saved)
                mailbox.mark_seen(item.uid)
            else:
                log.warning("send failed for %s (attempt %d), will retry: %s", key, attempts, exc)
                store.record(key, "send_failed", attempts=attempts, run_id=run_id, error=str(exc),
                             reply_eml=saved, decision=decision)
            continue

        store.record(key, "sent", attempts=store.attempts(key) + 1, run_id=run_id, decision=decision, reply_eml=saved)
        mailbox.mark_seen(item.uid)
        log.info("replied to %s (%s, run %s)", parsed.reply_address, decision, run_id)
    return handled


def run_watch(
    settings: Settings,
    make_mailbox: Callable[[], Mailbox],
    send: Sender,
    llm: JsonLLM | None,
    *,
    once: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
    config_path: str | None = None,
) -> int:
    """Poll until interrupted (or once). Connection problems are logged and retried, never fatal."""
    cfg = load_agent_config(config_path)["watch"]
    store = ProcessedStore(Path(settings.runs_dir) / str(cfg["processed_store_file"]))
    max_attempts = int(cfg["max_send_attempts"])
    started_at = datetime.now(timezone.utc)
    mailbox: Mailbox | None = None
    try:
        while True:
            try:
                if mailbox is None:
                    mailbox = make_mailbox()
                poll_once(
                    mailbox, send, settings, llm, store,
                    max_send_attempts=max_attempts,
                    started_at=started_at,
                    lookback_days=int(cfg["lookback_days"]),
                    start_grace_minutes=int(cfg["start_grace_minutes"]),
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.error("poll failed, reconnecting next time: %s", exc)
                if mailbox is not None:
                    try:
                        mailbox.close()
                    except Exception:
                        pass
                mailbox = None
            if once:
                return 0
            sleep_fn(float(settings.poll_seconds))
    except KeyboardInterrupt:
        log.info("stopped")
        return 0
    finally:
        if mailbox is not None:
            mailbox.close()
