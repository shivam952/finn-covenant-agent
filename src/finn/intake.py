"""Email intake: the LLM turns the free-text email into a fixed OrderIntent; code decides what to do.

Headers and attachments are read by code (they are structured). Only the body is free text,
so only the body is interpreted by the LLM, and only into this schema. If the LLM fails, a
structural fallback is used (an .xlsx attachment means "covenant check"), so a mail is never
left unanswered.
"""

from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from finn.config import PROJECT_ROOT
from finn.llm import JsonLLM
from finn.mail.inbox import ParsedEmail

DEFAULT_INTAKE_PROMPT = PROJECT_ROOT / "prompts" / "intake.txt"
DEFAULT_AGENT_CONFIG = PROJECT_ROOT / "config" / "agent.toml"


@lru_cache(maxsize=4)
def load_agent_config(path: str | None = None) -> dict[str, Any]:
    """Load config/agent.toml."""
    return tomllib.loads(Path(path or DEFAULT_AGENT_CONFIG).read_text(encoding="utf-8"))


class OrderIntent(BaseModel):
    """What the sender wants, as stated in the email (filled by the LLM, validated by Pydantic)."""

    intent: Literal["covenant_check", "acknowledgement", "other", "unclear"]
    plan_year: int | None = None
    test_date: str | None = None
    focus_contracts: list[str] = Field(default_factory=list)
    reply_language: Literal["de", "en"] = "de"
    salutation: str | None = None
    unsupported_requests: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    deadline_mentioned: str | None = None


@dataclass
class IntakeOutcome:
    intent: OrderIntent
    source: str  # "llm" or "fallback"
    meta: dict[str, Any] = field(default_factory=dict)


def xlsx_attachments(parsed: ParsedEmail) -> list[str]:
    return [a.filename for a in parsed.attachments if a.filename.lower().endswith(".xlsx")]


def read_intent(
    parsed: ParsedEmail,
    llm: JsonLLM | None,
    default_lang: str,
    *,
    prompt_path: Path | None = None,
    config_path: str | None = None,
) -> IntakeOutcome:
    """Ask the LLM for the OrderIntent; fall back to a structural default on any failure."""
    cfg = load_agent_config(config_path)
    max_chars = int(cfg["intake"]["max_body_chars"])
    fallback = OrderIntent(
        intent="covenant_check" if xlsx_attachments(parsed) else "unclear",
        reply_language="en" if default_lang.lower().startswith("en") else "de",
    )
    if llm is None:
        return IntakeOutcome(fallback, "fallback", {"reason": "no LLM configured"})

    user = json.dumps(
        {
            "from": parsed.sender,
            "subject": parsed.subject,
            "attachments": [a.filename for a in parsed.attachments],
            "body": parsed.body_text[:max_chars],
        },
        ensure_ascii=False,
        indent=2,
    )
    system = (prompt_path or DEFAULT_INTAKE_PROMPT).read_text(encoding="utf-8")
    try:
        intent, meta = llm.complete_json(system=system, user=user, schema=OrderIntent, purpose="intake")
    except Exception as exc:  # intake must never block a reply
        return IntakeOutcome(fallback, "fallback", {"error": f"{type(exc).__name__}: {exc}"})
    return IntakeOutcome(intent, "llm", meta)


@dataclass
class Decision:
    """What Finn does with the mail. Decided by code from headers, attachments and the intent."""

    kind: Literal[
        "run",
        "missing_attachment",
        "wrong_attachment",
        "unrelated",
        "needs_clarification",
        "no_reply",
    ]
    params: dict[str, Any] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)


def decide(parsed: ParsedEmail, intent: OrderIntent) -> Decision:
    """Fixed rules over structured data only; no keyword matching on free text."""
    if intent.intent == "acknowledgement" and not xlsx_attachments(parsed):
        answer = bool(load_agent_config()["intake"].get("answer_acknowledgements", False))
        return Decision("unrelated") if answer else Decision("no_reply")
    if intent.intent == "other":
        return Decision("unrelated")
    if xlsx_attachments(parsed):
        return Decision("run")
    if intent.intent == "unclear" and intent.missing_info:
        return Decision("needs_clarification", questions=list(intent.missing_info))
    others = [a.filename for a in parsed.attachments if a.filename]
    if others:
        return Decision("wrong_attachment", params={"filename": others[0]})
    return Decision("missing_attachment")


def is_automatic_or_own(parsed: ParsedEmail, own_address: str, config_path: str | None = None) -> str | None:
    """Reason to ignore this mail (own reply, auto-reply, bulk, daemon), or None to answer it."""
    cfg = load_agent_config(config_path)["loop_protection"]
    from email.utils import parseaddr

    sender_addr = parseaddr(parsed.sender)[1].lower()
    if own_address and sender_addr == parseaddr(own_address)[1].lower():
        return "own address"
    for name in cfg.get("ignore_if_header_present", []):
        if name.lower() in parsed.headers:
            return f"header {name}"
    for name, prefixes in cfg.get("ignore_if_header_value", {}).items():
        value = parsed.headers.get(name.lower(), "").strip().lower()
        if value and any(value.startswith(p.lower()) for p in prefixes):
            return f"header {name}: {value}"
    local = sender_addr.split("@")[0]
    if any(p.lower() in local for p in cfg.get("ignore_sender_contains", [])):
        return f"sender {sender_addr}"
    return None
