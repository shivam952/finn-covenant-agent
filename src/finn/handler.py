"""Handle one incoming email end to end. Always returns a reply (or an explicit 'ignore').

Order (fixed): loop protection -> intake (LLM, schema only) -> decision (code) ->
pipeline (code + LLM reading) -> reply (code + LLM summary) -> audit file.
Any exception inside becomes a polite error reply plus a full audit entry; nothing is silent.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from finn.audit import write_audit
from finn.config import Settings
from finn.intake import Decision, IntakeOutcome, decide, is_automatic_or_own, read_intent, xlsx_attachments
from finn.llm import JsonLLM
from finn.mail.inbox import ParsedEmail
from finn.mail.outbox import build_reply_message
from finn.pipeline import PlanInconsistentError, RunContext, RunOutcome, generate_run_id, run_check
from finn.plan.reader import PlanFormatError
from finn.render import load_texts, render_check
from finn.report import Reply, ReplyContext, build_reply, build_simple_reply, load_report_config


@dataclass
class Handled:
    """Result of handling one mail."""

    run_id: str
    ignored_reason: str | None = None
    decision: Decision | None = None
    intake: IntakeOutcome | None = None
    reply: Reply | None = None
    message: Any = None  # email.message.EmailMessage ready to send
    outcome: RunOutcome | None = None
    audit_path: Path | None = None
    error: str | None = None
    reply_lang: str | None = None
    files: dict[str, Path] = field(default_factory=dict)


def _intent_notes(intake: IntakeOutcome, outcome: RunOutcome, lang: str) -> list[str]:
    """Notes where the email and the file disagree (the file wins), and unsupported wishes."""
    texts = load_texts(lang)["intake"]
    notes: list[str] = []
    meta = outcome.report.plan_meta
    found_year = meta.get("plan_year")
    stated = intake.intent.plan_year
    if stated is not None and found_year is not None and int(stated) != int(found_year):
        notes.append(texts["plan_year_mismatch"].format(stated=stated, found=found_year))
    if intake.intent.test_date and meta.get("test_date"):
        try:
            stated_date = date.fromisoformat(intake.intent.test_date)
            d, m, y = (int(x) for x in str(meta["test_date"]).split("."))
            if stated_date != date(y, m, d):
                notes.append(
                    texts["test_date_mismatch"].format(
                        stated=stated_date.strftime("%d.%m.%Y"), found=meta["test_date"]
                    )
                )
        except ValueError:
            pass  # an unparsable date from the LLM is ignored, the file decides anyway
    for req in intake.intent.unsupported_requests:
        notes.append(texts["unsupported_request"].format(request=req))
    return notes


def _reply_all_cc(parsed: ParsedEmail, settings: Settings) -> list[str]:
    """Everyone on the original To/Cc except Finn and the reply recipient (if reply_all is on)."""
    from email.utils import formataddr, getaddresses, parseaddr

    from finn.intake import load_agent_config

    if not load_agent_config()["reply"].get("reply_all", False):
        return []
    skip = {
        parseaddr(a)[1].lower()
        for a in (settings.finn_mail_address, settings.mail_user, parsed.reply_address, parsed.sender)
        if a
    }
    out: list[str] = []
    for name, addr in getaddresses([parsed.headers.get("to", ""), parsed.headers.get("cc", "")]):
        if addr and addr.lower() not in skip and formataddr((name, addr)) not in out:
            out.append(formataddr((name, addr)))
            skip.add(addr.lower())
    return out


def handle_email(
    parsed: ParsedEmail,
    settings: Settings,
    llm: JsonLLM | None,
    *,
    run_id: str | None = None,
    lang_override: str | None = None,
) -> Handled:
    """Process one parsed email and build the reply message (sending is the caller's job)."""
    run_id = run_id or generate_run_id()
    h = Handled(run_id=run_id)
    run_dir = Path(settings.runs_dir) / run_id
    audit: dict[str, Any] = {
        "run_id": run_id,
        "email": {
            "from": parsed.sender,
            "reply_to": parsed.reply_to,
            "subject": parsed.subject,
            "message_id": parsed.message_id,
            "date": parsed.date,
            "body_text": parsed.body_text,
            "attachments": [a.filename for a in parsed.attachments],
        },
    }

    reason = is_automatic_or_own(parsed, settings.finn_mail_address)
    if reason is not None:
        h.ignored_reason = reason
        return h

    lang = lang_override or settings.reply_language
    try:
        intake = read_intent(parsed, llm, lang)
        h.intake = intake
        lang = lang_override or intake.intent.reply_language
        audit["intake"] = {"source": intake.source, "intent": intake.intent.model_dump(), "meta": intake.meta}
        decision = decide(parsed, intake.intent)
        h.decision = decision
        audit["decision"] = {"kind": decision.kind, "params": decision.params, "questions": decision.questions}
        ctx = ReplyContext(
            lang=lang,
            salutation=intake.intent.salutation,
            original_subject=parsed.subject,
            focus_contracts=list(intake.intent.focus_contracts),
            run_id=run_id,
        )

        if decision.kind == "no_reply":
            # A plain thank-you or confirmation: recorded, not answered (configurable in agent.toml).
            h.ignored_reason = "acknowledgement without a request"
            audit["reply"] = None
            h.audit_path = write_audit(audit, settings.runs_dir)
            return h
        if decision.kind != "run":
            h.reply = build_simple_reply(decision.kind, ctx, decision.params, decision.questions)
        else:
            run_dir.mkdir(parents=True, exist_ok=True)
            att = next(a for a in parsed.attachments if a.filename in xlsx_attachments(parsed))
            xlsx_path = run_dir / Path(att.filename).name
            xlsx_path.write_bytes(att.payload)
            try:
                rctx = RunContext.create(settings=settings, llm=llm, run_id=run_id) if llm is not None else None
                if rctx is None:
                    raise RuntimeError("no LLM configured for contract reading")
                # The year named in the email is used if the file has a plan column for it;
                # otherwise the file decides (and the mismatch is noted in the reply).
                outcome = None
                stated_year = intake.intent.plan_year
                if stated_year is not None:
                    try:
                        outcome = run_check(xlsx_path, rctx, plan_year=int(stated_year), lang=lang)
                    except PlanFormatError:
                        outcome = None
                if outcome is None:
                    outcome = run_check(xlsx_path, rctx, lang=lang)
            except PlanFormatError as exc:
                audit["plan_error"] = {"key": exc.key, "params": {k: str(v) for k, v in exc.params.items()}}
                h.reply = build_simple_reply("plan_format_error", ctx, {"message": exc.render(lang)})
            except PlanInconsistentError as exc:
                unit = str(exc.plan_meta.get("amount_unit", ""))
                lines = [render_check(c, lang=lang, unit=unit) for c in exc.failed]
                audit["plan_inconsistent"] = [c.model_dump(mode="json") for c in exc.failed]
                h.reply = build_simple_reply("plan_inconsistent", ctx, questions=lines)
            else:
                h.outcome = outcome
                audit.update({k: v for k, v in outcome.audit.items() if k not in ("run_id", "email")})
                outcome.report.warnings = _intent_notes(intake, outcome, lang) + outcome.report.warnings
                h.reply = build_reply(outcome.report, ctx, llm)
    except Exception as exc:  # never crash, never stay silent
        h.error = f"{type(exc).__name__}: {exc}"
        audit["error"] = {"message": h.error, "traceback": traceback.format_exc()}
        h.reply = build_simple_reply(
            "internal_error",
            ReplyContext(lang=lang, original_subject=parsed.subject, run_id=run_id),
            {"run_id": run_id},
        )

    assert h.reply is not None
    h.reply_lang = lang
    audit["reply"] = {"subject": h.reply.subject, "summary": h.reply.summary_meta, "text": h.reply.text}
    h.audit_path = write_audit(audit, settings.runs_dir)

    attachments: list[tuple[str, bytes, str]] = []
    if h.outcome is not None and bool(load_report_config()["report"].get("attach_audit", True)):
        att_name = load_texts(lang)["report"]["attachment_name"].format(run_id=run_id)
        attachments.append((att_name, h.audit_path.read_bytes(), "application/json"))
    h.message = build_reply_message(
        h.reply,
        from_addr=settings.finn_mail_address or settings.mail_user,
        from_name=load_texts(lang)["report"]["signature"],
        to_addr=parsed.reply_address,
        cc_addrs=_reply_all_cc(parsed, settings),
        in_reply_to=parsed.message_id,
        references=parsed.references,
        attachments=attachments,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    for name, content in (("reply.html", h.reply.html), ("reply.txt", h.reply.text)):
        (run_dir / name).write_text(content, encoding="utf-8")
        h.files[name] = run_dir / name
    (run_dir / "reply.eml").write_bytes(bytes(h.message))
    h.files["reply.eml"] = run_dir / "reply.eml"
    return h
