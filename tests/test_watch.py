"""`finn watch` loop: exactly-once replies, loop protection, send retries, reconnects. No network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from datetime import datetime, timedelta, timezone

from finn.config import Settings
from finn.mail.inbox import MailItem
from finn.intake import OrderIntent
from finn.watcher import ProcessedStore, poll_once, run_watch
from golden_llm import GoldenFakeLLM

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES = REPO_ROOT / "data" / "samples"
CHECK = OrderIntent(intent="covenant_check", salutation="Katrin Möller")


class FakeMailbox:
    def __init__(self, messages: dict[str, bytes], received_at: datetime | None = None,
                 initially_seen: set[str] | None = None) -> None:
        self.messages = dict(messages)
        self.seen: set[str] = set(initially_seen or set())
        self.received_at = received_at or datetime.now(timezone.utc)
        self.closed = False

    def fetch_recent(self, since: datetime) -> list[MailItem]:
        return [MailItem(uid=uid, raw=raw, received_at=self.received_at, seen=uid in self.seen)
                for uid, raw in self.messages.items()]

    def mark_seen(self, uid: str) -> None:
        self.seen.add(uid)

    def close(self) -> None:
        self.closed = True


def _settings(tmp_path: Path) -> Settings:
    return Settings(llm_api_key="x", llm_model="fake", contracts_dir=REPO_ROOT / "data" / "contracts",
                    cache_dir=tmp_path / "cache", runs_dir=tmp_path / "runs",
                    finn_mail_address="finn@example.com", poll_seconds=1)


def test_each_mail_answered_once_and_own_replies_ignored(tmp_path: Path) -> None:
    order = (SAMPLES / "order.eml").read_bytes()
    sent: list[Any] = []
    box = FakeMailbox({"1": order, "2": order})  # same Message-ID twice (e.g. a resend by the server)
    store = ProcessedStore(tmp_path / "runs" / "processed.json")
    llm = GoldenFakeLLM(intent=CHECK)

    poll_once(box, sent.append, _settings(tmp_path), llm, store, max_send_attempts=3)
    assert len(sent) == 1  # the duplicate Message-ID is skipped via the processed store

    # Finn's own reply lands in the inbox (e.g. Cc to itself): ignored, not answered.
    box.messages["3"] = bytes(sent[0])
    poll_once(box, sent.append, _settings(tmp_path), llm, store, max_send_attempts=3)
    assert len(sent) == 1

    # After a restart the store still knows the order.
    box2 = FakeMailbox({"9": order})
    poll_once(box2, sent.append, _settings(tmp_path), llm, ProcessedStore(store.path), max_send_attempts=3)
    assert len(sent) == 1


def test_failed_send_is_retried_then_given_up(tmp_path: Path) -> None:
    box = FakeMailbox({"1": (SAMPLES / "no_attachment.eml").read_bytes()})
    store = ProcessedStore(tmp_path / "p.json")
    calls: list[int] = []

    def flaky(_msg: Any) -> None:
        calls.append(1)
        raise OSError("smtp down")

    for _ in range(5):
        poll_once(box, flaky, _settings(tmp_path), GoldenFakeLLM(intent=CHECK), store, max_send_attempts=3)
    assert len(calls) == 3  # tried 3 times (the saved reply is resent, not rebuilt), then given up
    assert "1" in box.seen
    assert next(iter(store.data.values()))["status"] == "given_up"


def test_watch_survives_connection_errors(tmp_path: Path) -> None:
    attempts: list[int] = []

    def make_box() -> FakeMailbox:
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("imap down")
        return FakeMailbox({})

    polls: list[float] = []

    def sleep(seconds: float) -> None:
        polls.append(seconds)
        if len(polls) >= 2:
            raise KeyboardInterrupt

    code = run_watch(_settings(tmp_path), make_box, lambda m: None, GoldenFakeLLM(intent=CHECK), sleep_fn=sleep)
    assert code == 0 and len(attempts) == 2


def test_order_opened_by_someone_during_the_demo_is_still_answered(tmp_path: Path) -> None:
    order = (SAMPLES / "order.eml").read_bytes()
    box = FakeMailbox({"1": order}, initially_seen={"1"})  # already read in Gmail
    sent: list[Any] = []
    poll_once(box, sent.append, _settings(tmp_path), GoldenFakeLLM(intent=CHECK),
              ProcessedStore(tmp_path / "p.json"), max_send_attempts=3,
              started_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert len(sent) == 1


def test_old_read_mail_from_before_the_start_is_not_answered(tmp_path: Path) -> None:
    order = (SAMPLES / "order.eml").read_bytes()
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    box = FakeMailbox({"1": order}, received_at=old, initially_seen={"1"})
    sent: list[Any] = []
    poll_once(box, sent.append, _settings(tmp_path), GoldenFakeLLM(intent=CHECK),
              ProcessedStore(tmp_path / "p.json"), max_send_attempts=3,
              started_at=datetime.now(timezone.utc), start_grace_minutes=15)
    assert sent == []


def test_failed_send_resends_the_saved_reply_without_rerunning(tmp_path: Path) -> None:
    box = FakeMailbox({"1": (SAMPLES / "order.eml").read_bytes()})
    store = ProcessedStore(tmp_path / "p.json")
    outcomes: list[str] = []

    def flaky_once(msg: Any) -> None:
        if not outcomes:
            outcomes.append("fail")
            raise OSError("smtp hiccup")
        outcomes.append(msg["Subject"])

    llm = GoldenFakeLLM(intent=CHECK)
    first = poll_once(box, flaky_once, _settings(tmp_path), llm, store, max_send_attempts=3)
    second = poll_once(box, flaky_once, _settings(tmp_path), llm, store, max_send_attempts=3)
    assert len(first) == 1 and second == []  # nothing re-handled on the retry
    assert outcomes == ["fail", "Re: Covenant-Prüfung Wirtschaftsplan 2027"]
